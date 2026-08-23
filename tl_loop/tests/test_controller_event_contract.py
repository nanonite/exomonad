from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_controller_event_contract",
    PROJECT_ROOT / "scripts/check_controller_event_contract.py",
)
assert SPEC and SPEC.loader
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def test_controller_event_contract_matches_all_payload_builders() -> None:
    assert CHECKER.check(PROJECT_ROOT) == []


def test_dispatch_payload_drift_is_reported(tmp_path: Path) -> None:
    shutil.copytree(PROJECT_ROOT / "tl_loop", tmp_path / "tl_loop")
    shutil.copytree(PROJECT_ROOT / "docs", tmp_path / "docs")
    driver = tmp_path / "tl_loop/loop/driver.py"
    driver.write_text(
        driver.read_text(encoding="utf-8").replace(
            chr(10).join(
                (
                    '        "attempt": attempt.attempt,',
                    "",
                )
            ),
            chr(10).join(
                (
                    '        "attempt": attempt.attempt,',
                    '        "unregistered_dimension": "drift",',
                    "",
                )
            ),
            1,
        ),
        encoding="utf-8",
    )

    errors = CHECKER.check(tmp_path)

    assert any("unregistered_dimension" in error for error in errors)


@pytest.mark.parametrize(
    "event_type",
    (
        "tl.dispatch_intended",
        "tl.spawn_requested",
        "tl.spawn_request_accepted",
        "tl.spawn_request_failed",
        "tl.dispatch_confirmed",
        "tl.dispatch_reconciliation_started",
        "tl.dispatch_reconciliation_completed",
    ),
)
def test_dispatch_events_share_attempt_field(event_type: str) -> None:
    contract = CHECKER._contract(PROJECT_ROOT)
    assert "attempt" in contract[event_type]
