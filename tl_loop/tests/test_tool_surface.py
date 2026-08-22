from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_tool_surface", PROJECT_ROOT / "scripts/check_tool_surface.py"
)
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def test_source_derived_tool_surface_is_complete() -> None:
    assert CHECKER.check_surface(PROJECT_ROOT) == []


def test_unregistered_tool_is_reported(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(PROJECT_ROOT / "haskell", tmp_path / "haskell")
    shutil.copytree(PROJECT_ROOT / ".exo", tmp_path / ".exo")
    shutil.copytree(PROJECT_ROOT / "tl_loop", tmp_path / "tl_loop")
    role = tmp_path / ".exo/roles/devswarm/TLRole.hs"
    role.write_text(
        role.read_text().replace(
            "resolveLivePrForSlice = mkHandler @ResolveLivePrForSlice,\n", ""
        )
    )
    errors = CHECKER.check_surface(tmp_path)
    assert any("resolve_live_pr_for_slice" in error for error in errors)
