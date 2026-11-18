#!/usr/bin/env python3
"""Structural guard for the Chainlink #1433 R1 preflight proof.

The compiler already makes a skipped preflight a hard error: the controller
release entry point takes an ``AcceptedRoleConfiguration`` value whose field is
private to the module that owns the one comparison function.  This guard
catches the remaining textual bypasses a compiler cannot see (for example a
second call site that would construct the proof some other way) and, when the
recorded evidence directory is available, verifies the red-on-old-code logs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PRODUCTION_MARKER = "\n#[cfg(test)]\nmod tests {"


class GuardError(AssertionError):
    pass


def production_source(source_root: Path) -> str:
    source = (source_root / "rust/exomonad/src/init.rs").read_text(encoding="utf-8")
    marker = source.find(PRODUCTION_MARKER)
    if marker < 0:
        raise GuardError("could not locate the production/test source boundary")
    return source[:marker]


def test_the_proof_type_owns_a_private_identity_field(source: str) -> None:
    if "mod accepted_role_configuration {" not in source:
        raise GuardError("the accepted-role-configuration proof module is missing")
    if "pub struct AcceptedRoleConfiguration {\n        identity: String," not in source:
        raise GuardError("the proof identity field must stay private to its module")


def test_the_preflight_is_the_only_proof_producer(source: str) -> None:
    start = source.find("mod accepted_role_configuration {")
    end = source.find("/// The single acceptance rule", start)
    if start < 0 or end < 0:
        raise GuardError("the proof module boundaries are not recognisable")
    module = source[start:end]
    if module.count("Ok(AcceptedRoleConfiguration {") != 1:
        raise GuardError("the proof must be constructed exactly once, after acceptance")
    constructions = source.count("AcceptedRoleConfiguration {")
    if constructions != 5:
        raise GuardError(
            "unexpected AcceptedRoleConfiguration construction outside the proof module"
        )


def test_both_release_entry_points_call_the_one_preflight(source: str) -> None:
    occurrences = source.count("ensure_server_accepts_role_configuration(")
    if occurrences != 3:
        raise GuardError(
            "expected the definition plus exactly two release-entry call sites, "
            f"found {occurrences}"
        )
    if source.count("ensure_server_accepts_role_configuration(&cwd, &config)") != 1:
        raise GuardError("the fresh-session release entry point no longer runs the preflight")
    if source.count("ensure_server_accepts_role_configuration(project_dir, config)") != 1:
        raise GuardError("the existing-session release entry point no longer runs the preflight")


def test_controller_release_requires_the_proof(source: str) -> None:
    signature = "accepted_role_configuration: &AcceptedRoleConfiguration"
    if signature not in source:
        raise GuardError("launch_tl_recovery does not require the preflight proof")
    launch = source.find("async fn launch_tl_recovery(")
    body = source[launch : source.find("\n}\n", launch)]
    if "ensure_server_accepts_role_configuration(" in body:
        raise GuardError(
            "launch_tl_recovery must not run its own preflight; the proof comes from the caller"
        )


def test_the_proof_is_built_only_after_the_comparison(source: str) -> None:
    start = source.find("pub async fn ensure_server_accepts_role_configuration")
    end = source.find("/// Test-only proof", start)
    body = source[start:end]
    compare = body.find("compare_role_configuration_identities(")
    construct = body.find("Ok(AcceptedRoleConfiguration {")
    if compare < 0 or construct < 0 or compare > construct:
        raise GuardError("the proof must be constructed only after the identities compare equal")


def test_recorded_red_on_old_code_evidence(evidence_dir: Path) -> list[str]:
    expected = {
        "old-code-red-r1-launch-preflight-removed.log": (
            "accepted_role_configuration",
            "error",
        ),
        "old-code-red-r1-reconcile-preflight-removed.log": (
            "accepted_role_configuration",
            "error",
        ),
        "old-code-red-r1-compare-always-accept.log": (
            "preflight_yields_no_proof_for_a_different_server_identity",
            "FAILED",
        ),
    }
    cited: list[str] = []
    for name, needles in expected.items():
        path = evidence_dir / name
        if not path.is_file():
            raise GuardError(f"missing red-on-old-code evidence: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            if needle not in text:
                raise GuardError(f"{path} does not contain the expected failure marker {needle!r}")
        cited.append(str(path))
    return cited


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)

    source = production_source(args.source)
    checks = [
        test_the_proof_type_owns_a_private_identity_field,
        test_the_preflight_is_the_only_proof_producer,
        test_both_release_entry_points_call_the_one_preflight,
        test_controller_release_requires_the_proof,
        test_the_proof_is_built_only_after_the_comparison,
    ]
    for check in checks:
        check(source)
        print(f"PASS: {check.__name__}")

    if args.evidence_dir is not None:
        for path in test_recorded_red_on_old_code_evidence(args.evidence_dir):
            print(f"PASS: cited red-on-old-code evidence {path}")
    else:
        print("NOTE: no evidence directory supplied; red-on-old-code logs were not re-read")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except GuardError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
