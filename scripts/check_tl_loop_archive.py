"""Validate and import every non-test module from the packaged TL controller."""

import argparse
import importlib
import importlib.util
import json
import pkgutil
import sys
import zipfile
from pathlib import Path


class ArchiveFingerprintError(RuntimeError):
    """The archive was not built from the supplied source tree."""


def _source_fingerprint(source: Path) -> dict[str, object]:
    module_path = source / "fingerprint.py"
    spec = importlib.util.spec_from_file_location(
        "_tl_loop_source_fingerprint", module_path
    )
    if spec is None or spec.loader is None:
        raise ArchiveFingerprintError(
            f"source fingerprint module is missing: {module_path}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.source_fingerprint(source)


def check_archive_fingerprint(archive: Path, source: Path) -> None:
    """Fail closed when an archive stamp differs from current source."""
    fingerprint_filename = "_build_fingerprint.json"

    try:
        with zipfile.ZipFile(archive) as package:
            actual = json.loads(package.read(f"tl_loop/{fingerprint_filename}"))
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise ArchiveFingerprintError(
            f"{archive} has no valid {fingerprint_filename}"
        ) from error
    expected = _source_fingerprint(source)
    if actual != expected:
        raise ArchiveFingerprintError(
            f"stale TL controller archive {archive}: expected {expected}, found {actual}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--source", type=Path, default=Path.cwd() / "tl_loop")
    arguments = parser.parse_args(argv)
    archive = arguments.archive.expanduser().resolve()
    source = arguments.source.expanduser().resolve()
    sys.path.insert(0, str(archive))
    try:
        check_archive_fingerprint(archive, source)
    except ArchiveFingerprintError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    import tl_loop

    for module in pkgutil.walk_packages(tl_loop.__path__, tl_loop.__name__ + "."):
        if ".tests" not in module.name and not module.name.rsplit(".", 1)[
            -1
        ].startswith("test"):
            importlib.import_module(module.name)


if __name__ == "__main__":
    main()
