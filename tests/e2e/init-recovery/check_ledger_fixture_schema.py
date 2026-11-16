#!/usr/bin/env python3
"""Docker-free schema parity check for seeded init-recovery ledger rows.

The server parses every ledger segment with the Rust ``LedgerEvent`` struct in
``rust/exomonad-core/src/services/immutable_ledger.rs``. A fixture that seeds a
row missing one of that struct's required fields makes the next server startup
fail to replay the ledger, which blocks provisioning before any acceptance
phase can run.

This check:
  * derives the required envelope fields from the Rust struct,
  * asserts the fixture declares the same required set,
  * seeds the historical blocked-resume chronology into a throwaway repository
    and validates every row the seed actually wrote, and
  * asserts the seed appends to the writer's current segment instead of
    inventing a segment named after a ``run_seq`` value.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUST_LEDGER = (
    PROJECT_ROOT / "rust/exomonad-core/src/services/immutable_ledger.rs"
)
FIXTURE = Path(__file__).resolve().with_name("configured-owner-resume.py")

EXISTING_SEGMENT = "segment-000000000000.jsonl"
SUCCESSOR_ID = "f285efc5-4b0d-4a42-bcf8-c50a329f4a1d"
PREDECESSOR_ID = "f4460477-40bf-4824-941b-dcc3d0ebc9a1"


def load_fixture() -> Any:
    """Load the fixture module without running its CLI."""
    spec = importlib.util.spec_from_file_location(
        "configured_owner_resume_fixture", FIXTURE
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load fixture module at {FIXTURE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rust_required_fields() -> tuple[set[str], set[str]]:
    """Return the required and optional serialized fields of ``LedgerEvent``."""
    source = RUST_LEDGER.read_text(encoding="utf-8")
    match = re.search(
        r"pub struct LedgerEvent \{(?P<body>.*?)\n\}", source, re.DOTALL
    )
    if match is None:
        raise SystemExit(f"cannot locate LedgerEvent in {RUST_LEDGER}")
    required: set[str] = set()
    optional: set[str] = set()
    attributes: list[str] = []
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("#["):
            attributes.append(line)
            continue
        field = re.match(r"pub (\w+): (.+),$", line)
        if field is None:
            continue
        name, field_type = field.groups()
        attribute_text = " ".join(attributes)
        attributes = []
        renamed = re.search(r'rename\s*=\s*"([^"]+)"', attribute_text)
        json_name = renamed.group(1) if renamed else name
        if field_type.startswith("Option<") or "serde(default" in attribute_text:
            optional.add(json_name)
        else:
            required.add(json_name)
    if not required:
        raise SystemExit("derived an empty LedgerEvent required-field set")
    return required, optional


def assert_fixture_schema_parity(fixture: Any, required: set[str]) -> None:
    declared = getattr(fixture, "LEDGER_EVENT_REQUIRED_FIELDS", None)
    if declared is None:
        # Pre-fix fixtures have no declared set; the Rust-derived set is the
        # only authority and the seeded rows below still have to satisfy it.
        return
    declared_set = set(declared)
    if declared_set != required:
        raise SystemExit(
            "fixture LEDGER_EVENT_REQUIRED_FIELDS diverges from the Rust "
            f"LedgerEvent required fields: fixture-only={sorted(declared_set - required)} "
            f"rust-only={sorted(required - declared_set)}"
        )


def seed_existing_segment(repository: Path) -> None:
    segment_dir = repository / ".exo/ledger/segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    existing_row = {
        "schema_version": 1,
        "id": "fixture-existing-row",
        "event_id": "fixture-existing-row",
        "event_time": "2026-11-15T00:00:00.000Z",
        "observed_at": "2026-11-15T00:00:00.000Z",
        "run_seq": 5,
        "type": "agent.invocation.started",
        "agent_id": "kani",
        "run_id": "run-1351-fixture",
        "session_id": "fixture-session",
        "invocation_id": "fixture-invocation",
        "generation": 1,
        "source": "lifecycle",
        "lifecycle_state": "emitted",
        "data": {},
    }
    (segment_dir / EXISTING_SEGMENT).write_text(
        json.dumps(existing_row) + "\n", encoding="utf-8"
    )
    (repository / ".exo/run_id").write_text("run-1351-fixture\n", encoding="utf-8")
    (repository / ".exo/ledger/run_seq").write_text("5\n", encoding="utf-8")


def seed_worktree(path: Path) -> None:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
    )


def validate_rows(
    repository: Path, required: set[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    segment_dir = repository / ".exo/ledger/segments"
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    for segment in sorted(segment_dir.glob("segment-*.jsonl")):
        for line_number, line in enumerate(
            segment.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                failures.append(f"{segment.name}:{line_number} invalid JSON: {error}")
                continue
            rows.append(row)
            missing = sorted(name for name in required if name not in row)
            if missing:
                failures.append(
                    f"{segment.name}:{line_number} {row.get('type')!r} "
                    f"missing required fields {missing}"
                )
            if row.get("id") != row.get("event_id"):
                failures.append(
                    f"{segment.name}:{line_number} {row.get('type')!r} "
                    "has a mismatched id/event_id"
                )
            if not isinstance(row.get("data"), dict):
                failures.append(
                    f"{segment.name}:{line_number} {row.get('type')!r} "
                    "has a non-object data payload"
                )
    return failures, rows


def call_seed(fixture: Any, repository: Path, worktree: Path) -> None:
    kwargs = {
        "owner_name": "issue-1351-fixture-owner",
        "controller_run_id": "kani",
        "successor_id": SUCCESSOR_ID,
        "predecessor_id": PREDECESSOR_ID,
        "branch": "main.fixture",
        "worktree": str(worktree),
        "ended_at": int(time.time()) - 1,
    }
    # The pre-fix seed took no controller_run_id; filtering keeps this check
    # runnable against the old fixture so the red result is the schema failure
    # rather than an argument error.
    parameters = inspect.signature(
        fixture._seed_legacy_successor_lifecycle
    ).parameters
    fixture._seed_legacy_successor_lifecycle(
        repository, **{name: value for name, value in kwargs.items() if name in parameters}
    )


def assert_historical_shape(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if any(row.get("type") == "agent.spawned" for row in rows):
        failures.append("seeded historical chronology invented an agent.spawned row")
    for row in rows:
        if row.get("type") != "agent.resumed":
            continue
        if "intent_id" in row.get("data", {}):
            failures.append("seeded agent.resumed row invented an intent_id")
    return failures


def main() -> None:
    required, optional = rust_required_fields()
    overlap = sorted(required & optional)
    if overlap:
        raise SystemExit(f"LedgerEvent fields are both required and optional: {overlap}")
    fixture = load_fixture()
    assert_fixture_schema_parity(fixture, required)
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ledger-fixture-schema-") as temporary:
        root = Path(temporary)
        repository = root / "repo"
        worktree = root / "worktree"
        seed_existing_segment(repository)
        seed_worktree(worktree)
        call_seed(fixture, repository, worktree)
        row_failures, rows = validate_rows(repository, required)
        failures.extend(row_failures)
        failures.extend(assert_historical_shape(rows))
        segments = sorted(
            path.name
            for path in (repository / ".exo/ledger/segments").glob("segment-*.jsonl")
        )
        if segments != [EXISTING_SEGMENT]:
            failures.append(
                "seeded ledger rows invented a segment layout instead of "
                f"appending to the writer's current segment: {segments}"
            )
        seeded = [
            row
            for row in rows
            if row.get("invocation_id") == SUCCESSOR_ID
            and row.get("type")
            in {"agent.invocation.started", "agent.resumed", "agent.invocation.finished"}
        ]
        if len(seeded) != 3:
            failures.append(
                f"seeded chronology should contain three rows for {SUCCESSOR_ID}, "
                f"found {len(seeded)}"
            )
    if failures:
        raise SystemExit(
            "seeded fixture ledger rows violate the Rust LedgerEvent schema:\n  "
            + "\n  ".join(failures)
        )
    print(
        "OK: fixture ledger rows satisfy the Rust LedgerEvent schema and append "
        "to the writer's current segment"
    )


if __name__ == "__main__":
    main()
