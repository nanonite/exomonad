"""Check the declaration, role-registration, and controller-call triangles."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolDeclaration:
    name: str
    source: Path
    operator_only: bool


TOOL_INSTANCE = re.compile(r"instance MCPTool\s+([A-Za-z0-9_']+)\s+where")
TOOL_NAME = re.compile(r'toolName\s*=\s*"([^"]+)"')
HANDLER_TYPE = re.compile(r"mkHandler\s+@([A-Za-z0-9_']+)")


def declared_tools(tool_root: Path) -> dict[str, ToolDeclaration]:
    declarations: dict[str, ToolDeclaration] = {}
    for source in sorted(tool_root.glob("*.hs")):
        text = source.read_text()
        matches = list(TOOL_INSTANCE.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            name_match = TOOL_NAME.search(text, match.end(), end)
            if name_match is None:
                raise ValueError(f"{source}: MCPTool {match.group(1)} has no toolName")
            declarations[match.group(1)] = ToolDeclaration(
                name=name_match.group(1),
                source=source,
                operator_only="exomonad-role: operator-only"
                in text[max(0, match.start() - 256) : end],
            )
    return declarations


def registered_types(role_root: Path) -> dict[str, set[str]]:
    return {
        role.name: set(HANDLER_TYPE.findall(role.read_text()))
        for role in sorted(role_root.glob("*Role.hs"))
    }


def python_constant_strings(source: Path, names: set[str]) -> set[str]:
    tree = ast.parse(source.read_text(), filename=str(source))
    values: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id in names and value is not None:
            values.update(
                constant.value
                for constant in ast.walk(value)
                if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
            )
    return values


def check_surface(project_root: Path) -> list[str]:
    tool_root = project_root / "haskell/wasm-guest/src/ExoMonad/Guest/Tools"
    role_root = project_root / ".exo/roles/devswarm"
    declarations = declared_tools(tool_root)
    declarations.update(declared_tools(role_root))
    roles = registered_types(role_root)
    all_registered = set().union(*roles.values()) if roles else set()
    errors: list[str] = []

    for tool_type, declaration in sorted(declarations.items()):
        if not declaration.operator_only and tool_type not in all_registered:
            errors.append(
                f"{declaration.source}: {tool_type} ({declaration.name}) is not registered by any role"
            )

    controller_names = python_constant_strings(
        project_root / "tl_loop/client/effects.py", {"TOOL_METHODS"}
    ) | python_constant_strings(
        project_root / "tl_loop/client/readonly.py", {"READ_METHODS"}
    )
    tl_types = roles.get("TLRole.hs", set())
    tl_names = {
        declarations[tool_type].name
        for tool_type in tl_types
        if tool_type in declarations
    }
    for name in sorted(controller_names - tl_names):
        errors.append(
            f"controller-callable tool {name!r} is not exposed by "
            ".exo/roles/devswarm/TLRole.hs"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    errors = check_surface(args.project_root.resolve())
    if errors:
        print("Tool surface check failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    declarations = declared_tools(
        args.project_root / "haskell/wasm-guest/src/ExoMonad/Guest/Tools"
    )
    declarations.update(
        declared_tools(args.project_root / ".exo/roles/devswarm")
    )
    print(f"Tool surface check passed ({len(declarations)} declarations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
