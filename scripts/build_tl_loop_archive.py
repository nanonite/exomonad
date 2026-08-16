"""Build the stdlib-only TL controller as a package-preserving zipapp."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

EXCLUDED_NAMES = {".venv", "tests", "__pycache__"}


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED_NAMES or name.endswith(".pyc")
    }


def build_archive(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        stage = Path(temporary_directory)
        shutil.copytree(source, stage / "tl_loop", ignore=_ignore)
        (stage / "__main__.py").write_text(
            "from tl_loop.__main__ import main" + chr(10)
            + "import sys" + chr(10)
            + "sys.exit(main())" + chr(10),
            encoding="utf-8",
        )
        zipapp.create_archive(
            stage,
            target=output,
            interpreter="/usr/bin/env python3",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build_archive(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
