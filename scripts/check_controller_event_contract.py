"""Cross-validate Python controller event payloads against the shared contract."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVENT_FILES = (
    "tl_loop/__main__.py",
    "tl_loop/loop/abandon.py",
    "tl_loop/loop/driver.py",
    "tl_loop/loop/escalate.py",
    "tl_loop/loop/heartbeat.py",
)
EVENT_CALL_NAMES = {"_record_controller_event", "emit_controller_event"}


@dataclass(frozen=True)
class EventUse:
    path: Path
    line: int
    event_type: str | None
    fields: frozenset[str] | None


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path}: expected an object")
    return value


def _contract(root: Path) -> dict[str, frozenset[str]]:
    source = _load_json(root / "docs/observability/controller-event-contract.v1.json")
    if source.get("contract_id") != "exomonad.controller-event-fields":
        raise ValueError("controller event contract has an unexpected id")
    if source.get("version") != 1 or not isinstance(source.get("events"), dict):
        raise ValueError("controller event contract must be version 1 with events")

    events: dict[str, frozenset[str]] = {}
    for event_type, definition in source["events"].items():
        if not isinstance(event_type, str) or not isinstance(definition, dict):
            raise TypeError("controller event contract contains an invalid event")
        fields = definition.get("fields")
        if (
            not isinstance(fields, list)
            or not fields
            or any(not isinstance(field, str) or not field for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError(f"{event_type}: fields must be unique non-empty strings")
        array_fields = definition.get("string_array_fields", [])
        if not isinstance(array_fields, list) or not set(array_fields).issubset(fields):
            raise ValueError(f"{event_type}: invalid string_array_fields")
        events[event_type] = frozenset(fields)

    registry = _load_json(root / "docs/observability/event-registry.json")
    registry_types = {
        item.get("type")
        for item in registry.get("event_types", [])
        if isinstance(item, dict)
    }
    missing = sorted(set(events) - registry_types)
    if missing:
        raise ValueError(
            "event registry is missing controller contract types: " + ", ".join(missing)
        )
    return events


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_keys(node: ast.AST | None) -> frozenset[str] | None:
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for key in node.keys:
        value = _literal_string(key)
        if value is None:
            return None
        keys.add(value)
    return frozenset(keys)


def _function_return_fields(tree: ast.AST) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        bindings = _function_bindings(function)
        fields: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Return):
                keys = _fields_for(node.value, bindings, result)
                if keys is not None:
                    fields.update(keys)
        if fields:
            result[function.name] = frozenset(fields)
    return result


def _function_bindings(function: ast.FunctionDef) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bindings[node.target.id] = node.value
    return bindings


def _fields_for(
    node: ast.AST | None,
    bindings: dict[str, ast.AST],
    function_returns: dict[str, frozenset[str]],
    seen: set[str] | None = None,
) -> frozenset[str] | None:
    if node is None:
        return None
    direct = _dict_keys(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name):
        seen = set() if seen is None else seen
        if node.id in seen or node.id not in bindings:
            return None
        seen.add(node.id)
        return _fields_for(bindings[node.id], bindings, function_returns, seen)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in function_returns:
            return function_returns[node.func.id]
        if isinstance(node.func, ast.Name) and node.func.id == "cast" and node.args:
            return _fields_for(node.args[-1], bindings, function_returns, seen)
    return None


def _event_arguments(call: ast.Call) -> tuple[ast.AST | None, ast.AST | None]:
    function_name = (
        call.func.id
        if isinstance(call.func, ast.Name)
        else call.func.attr
        if isinstance(call.func, ast.Attribute)
        else None
    )
    if function_name == "_record_controller_event":
        return (
            call.args[1] if len(call.args) > 1 else None,
            call.args[2] if len(call.args) > 2 else None,
        )
    if function_name != "emit_controller_event":
        return None, None
    event = call.args[1] if len(call.args) > 1 else None
    payload = call.args[2] if len(call.args) > 2 else None
    for keyword in call.keywords:
        if keyword.arg == "event_type":
            event = keyword.value
        elif keyword.arg == "payload":
            payload = keyword.value
    return event, payload


def _event_uses(path: Path) -> list[EventUse]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function_returns = _function_return_fields(tree)
    parents: dict[ast.AST, ast.AST] = {}
    functions: dict[ast.FunctionDef, dict[str, ast.AST]] = {}
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        functions[function] = _function_bindings(function)
        for child in ast.walk(function):
            parents[child] = function

    uses: list[EventUse] = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        function_name = (
            call.func.id
            if isinstance(call.func, ast.Name)
            else call.func.attr
            if isinstance(call.func, ast.Attribute)
            else None
        )
        if function_name not in EVENT_CALL_NAMES:
            continue
        event_node, payload_node = _event_arguments(call)
        function: ast.AST | None = call
        while function not in functions and function in parents:
            function = parents[function]
        if isinstance(function, ast.FunctionDef) and function.name == "_record_controller_event":
            continue
        bindings = functions.get(function, {})
        uses.append(
            EventUse(
                path=path,
                line=call.lineno,
                event_type=_literal_string(event_node),
                fields=_fields_for(payload_node, bindings, function_returns),
            )
        )
    return uses


def check(root: Path) -> list[str]:
    contracts = _contract(root)
    errors: list[str] = []
    for relative in EVENT_FILES:
        path = root / relative
        for use in _event_uses(path):
            prefix = f"{use.path}:{use.line}"
            if use.event_type is None:
                errors.append(f"{prefix}: controller event type is not statically declared")
                continue
            allowed = contracts.get(use.event_type)
            if allowed is None:
                errors.append(f"{prefix}: {use.event_type} is absent from the shared contract")
                continue
            if use.fields is None:
                errors.append(
                    f"{prefix}: {use.event_type} payload fields are not statically declared"
                )
                continue
            unknown = sorted(use.fields - allowed)
            if unknown:
                errors.append(
                    f"{prefix}: {use.event_type} sends fields not in the contract: "
                    + ", ".join(unknown)
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    errors = check(args.project_root.resolve())
    if errors:
        print("Controller event contract check failed:")
        print(*[f"- {error}" for error in errors], sep=chr(10))
        return 1
    print("Controller event contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
