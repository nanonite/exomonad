"""Independently rehash the runtime artifacts retained by the dispatch acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()

    root = args.directory.resolve(strict=True)
    manifest_path = root / "manifest.json"
    sums_path = root / "SHA256SUMS"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise SystemExit(f"retained evidence is incomplete under {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("acceptance") != "DISPATCH-ALL PASS":
        raise SystemExit("the retained manifest does not describe a passing acceptance run")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise SystemExit("the retained manifest lists no runtime artifacts")

    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("the retained manifest has a malformed artifact record")
        relative = record.get("retained_path")
        if not isinstance(relative, str) or not relative:
            raise SystemExit("the retained manifest has no retained path")
        expected_execution = record.get("sha256")
        expected_retained = record.get("retained_sha256")
        if expected_execution != expected_retained:
            raise SystemExit(f"execution and retained hashes differ for {relative}")
        actual = digest(root / relative)
        if actual != expected_execution:
            raise SystemExit(
                f"retained artifact {relative} rehashes to {actual}, "
                f"not the recorded {expected_execution}"
            )
        print(f"REHASH {relative} {actual}")

    listed = sums_path.read_text(encoding="utf-8").splitlines()
    recorded = {line.split("  ", 1)[1] for line in listed if "  " in line}
    manifest_paths = {record["retained_path"] for record in records}
    if recorded != manifest_paths:
        raise SystemExit("SHA256SUMS does not match the manifest artifact list")
    print(
        json.dumps(
            {
                "acceptance": manifest["acceptance"],
                "artifacts": len(records),
                "source_commit": manifest.get("source_fingerprint", {}).get("commit"),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"independent artifact rehash failed: {error}") from error
    sys.exit(0)
