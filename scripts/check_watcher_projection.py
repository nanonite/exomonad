"""Reject direct reads of ownership fields outside the watcher projection."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

OWNERSHIP_FIELDS = frozenset(
    {"publication_ownership_verified", "publication_ownership_error"}
)


def violations(project_root: Path) -> list[str]:
    loop_root = project_root / "tl_loop/loop"
    errors: list[str] = []
    for source in sorted(loop_root.glob("*.py")):
        if source.name == "observation.py":
            continue
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr != "get" or len(node.args) < 1:
                    continue
                key = node.args[0]
                if isinstance(key, ast.Constant) and key.value in OWNERSHIP_FIELDS:
                    errors.append(
                        f"{source}:{node.lineno}: raw watcher field read {key.value!r}"
                    )
            elif isinstance(node, ast.Subscript):
                key = node.slice
                if isinstance(key, ast.Constant) and key.value in OWNERSHIP_FIELDS:
                    errors.append(
                        f"{source}:{node.lineno}: raw watcher field read {key.value!r}"
                    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    errors = violations(parser.parse_args().project_root.resolve())
    if errors:
        print("Watcher projection check failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Watcher projection check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
