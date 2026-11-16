"""Pin the single controller route-resolution seam for dispatch decisions.

A slice's harness/model/effort is resolved by ``select_agent_type`` (allowlist,
budget, capability, difficulty) plus ``select_model``/``select_model_for_difficulty``
(catalog tier) and bound to provenance by ``_dispatch_route_fields``. Only the
three dispatch paths may call those helpers:

* fresh and pre-execution dispatch: ``_prepare_spawn``
* recovered dispatch: ``_recovered_spawn_choice`` and
  ``_confirmed_spawn_route_state_fields``
* terminal-owner / blocked-leaf recovery: ``_terminal_owner_recovery_proof``
  and ``_route_fields_for_blocked_resume``

A new call site is a second resolution path. This test fails so the reviewer
must either move the new code through the seam or deliberately extend this
allowlist with an audit note.
"""

from __future__ import annotations

import ast
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[1] / "loop" / "driver.py"

RESOLUTION_CALLS = {
    "select_agent_type": {
        "_prepare_spawn",
        "_recovered_spawn_choice",
        "_terminal_owner_recovery_proof",
    },
    "select_model": {"_selected_spawn_model", "_prepare_spawn"},
    "select_model_for_difficulty": {"_selected_spawn_model", "_prepare_spawn"},
    "_dispatch_route_fields": {
        "_prepare_spawn",
        "_confirmed_spawn_route_state_fields",
        "_terminal_owner_recovery_proof",
        "_route_fields_for_blocked_resume",
    },
}


def _call_owners(tree: ast.AST) -> list[tuple[str, str, int]]:
    """Return ``(call_name, enclosing_function, line)`` for seam calls."""
    calls: list[tuple[str, str, int]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            if name in RESOLUTION_CALLS:
                calls.append((name, self.stack[-1] if self.stack else "<module>", node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    return calls


def test_dispatch_route_resolution_has_no_second_site() -> None:
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    calls = _call_owners(tree)

    unexpected = [
        f"{name} called from {enclosing} (line {line})"
        for name, enclosing, line in calls
        if enclosing not in RESOLUTION_CALLS[name]
    ]

    assert not unexpected, (
        "dispatch route resolution moved outside the single seam: "
        + "; ".join(unexpected)
    )


def test_every_dispatch_path_still_resolves_through_the_seam() -> None:
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    owners = {enclosing for _, enclosing, _ in _call_owners(tree)}

    assert {
        "_prepare_spawn",
        "_recovered_spawn_choice",
        "_confirmed_spawn_route_state_fields",
        "_terminal_owner_recovery_proof",
        "_route_fields_for_blocked_resume",
    } <= owners
