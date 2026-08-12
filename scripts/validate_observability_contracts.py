#!/usr/bin/env python3
"""Validate the versioned Phase 0 observability contracts and fixtures."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
REQUIRED_RULE_FIELDS = {
    "rule_id",
    "prerequisite_event",
    "required_event",
    "applicable_sources",
    "legacy_confidence_rule",
    "denominator_effect",
}
REQUIRED_SCENARIOS = {
    "multi_harness_session",
    "resume_retry_and_delivery",
    "review_and_merge_loop",
    "partial_sink_and_gap",
    "state_reconstruction_and_correction",
    "privacy_boundary_inputs",
}
REQUIRED_INVARIANTS = {f"I{index}" for index in range(1, 9)}


class ContractError(ValueError):
    """A contract or fixture violates the Phase 0 acceptance rules."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except json.JSONDecodeError as error:
        raise ContractError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{path}: top-level value must be an object")
    return value


def require_keys(value: dict[str, Any], keys: set[str], context: str) -> None:
    missing = sorted(keys - value.keys())
    if missing:
        raise ContractError(f"{context}: missing keys: {', '.join(missing)}")


def validate_registry(registry: dict[str, Any]) -> set[str]:
    require_keys(
        registry,
        {"registry_id", "version", "schema_version", "compatibility", "envelope", "payload_classes", "event_types"},
        "event registry",
    )
    if registry["version"] != 1 or registry["schema_version"] != 1:
        raise ContractError("event registry: version and schema_version must both be 1")

    compatibility = registry["compatibility"]
    require_keys(
        compatibility,
        {"canonical_identifier", "legacy_identifier", "new_event_identifier_rule", "reader_preference"},
        "event registry compatibility",
    )
    if compatibility["canonical_identifier"] != "event_id":
        raise ContractError("event registry: event_id must be canonical")
    if compatibility["legacy_identifier"] != "id":
        raise ContractError("event registry: id must remain the legacy alias")
    if "id equals event_id" not in compatibility["new_event_identifier_rule"]:
        raise ContractError("event registry: new rows must set id equal to event_id")

    envelope = registry["envelope"]
    require_keys(
        envelope,
        {"required_fields", "legacy_fields", "lifecycle_states", "sources", "identity_confidence"},
        "event registry envelope",
    )
    required_fields = envelope["required_fields"]
    if len(required_fields) != len(set(required_fields)):
        raise ContractError("event registry: required envelope fields must be unique")
    required_field_set = set(required_fields)
    if not {"schema_version", "event_id", "id", "event_time", "observed_at", "run_seq", "type", "data"}.issubset(required_field_set):
        raise ContractError("event registry: required envelope is missing an analysis field")
    if set(envelope["lifecycle_states"]) != {"observed", "emitted", "delivered", "consumed", "authoritative"}:
        raise ContractError("event registry: lifecycle states do not match the frozen contract")
    if "legacy" not in envelope["identity_confidence"] or "unknown" not in envelope["identity_confidence"]:
        raise ContractError("event registry: legacy and unknown identity confidence are required")

    payload_classes = set(registry["payload_classes"])
    event_types = registry["event_types"]
    if not event_types:
        raise ContractError("event registry: event_types cannot be empty")
    names: list[str] = []
    sources = set(envelope["sources"])
    for index, event in enumerate(event_types):
        context = f"event registry event_types[{index}]"
        require_keys(event, {"type", "payload_class", "producers"}, context)
        event_type = event["type"]
        if not isinstance(event_type, str) or not EVENT_TYPE_PATTERN.fullmatch(event_type):
            raise ContractError(f"{context}: invalid event type {event_type!r}")
        names.append(event_type)
        if event["payload_class"] not in payload_classes:
            raise ContractError(f"{context}: unknown payload class")
        if not event["producers"] or not set(event["producers"]).issubset(sources):
            raise ContractError(f"{context}: producers must be non-empty declared sources")
    if len(names) != len(set(names)):
        raise ContractError("event registry: event type names must be unique")

    dynamic_rule = registry.get("dynamic_event_rule", {})
    require_keys(dynamic_rule, {"allowed_namespace", "default_payload_class", "requires_declared_producer"}, "dynamic event rule")
    if dynamic_rule["default_payload_class"] not in payload_classes:
        raise ContractError("dynamic event rule: default payload class is not registered")
    return set(names)


def validate_expected_events(contract: dict[str, Any], event_types: set[str]) -> set[str]:
    require_keys(contract, {"contract_id", "version", "schema_version", "unit", "rules"}, "expected-event contract")
    if contract["version"] != 1 or contract["schema_version"] != 1 or contract["unit"] != "session":
        raise ContractError("expected-event contract: version, schema_version, or unit is invalid")
    rule_ids: set[str] = set()
    for index, rule in enumerate(contract["rules"]):
        context = f"expected-event rules[{index}]"
        require_keys(rule, REQUIRED_RULE_FIELDS, context)
        if rule["rule_id"] in rule_ids:
            raise ContractError(f"{context}: duplicate rule_id")
        rule_ids.add(rule["rule_id"])
        for event_key in ("prerequisite_event", "required_event"):
            event_type = rule[event_key]
            alternatives = event_type.split("/")
            if not event_type or not all(candidate in event_types for candidate in alternatives):
                raise ContractError(f"{context}: undeclared {event_key} {event_type}")
        if "allowed_delay_ms" not in rule and "allowed_window" not in rule:
            raise ContractError(f"{context}: allowed delay or window is required")
        if not rule["applicable_sources"]:
            raise ContractError(f"{context}: applicable_sources cannot be empty")
        if "correlation_fields" in rule and not all(
            isinstance(field, str) and field for field in rule["correlation_fields"]
        ):
            raise ContractError(f"{context}: correlation_fields must contain names")
        if "required_data" in rule and (
            not isinstance(rule["required_data"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in rule["required_data"].items())
        ):
            raise ContractError(f"{context}: required_data must map string fields to strings")
        if "prerequisite_data" in rule and (
            not isinstance(rule["prerequisite_data"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in rule["prerequisite_data"].items())
        ):
            raise ContractError(f"{context}: prerequisite_data must map string fields to strings")
    expected_rules = {
        "spawn_requires_invocation_start",
        "invocation_requires_finish",
        "parent_notification_requires_delivery",
        "delivery_requires_consumption_observation",
        "published_pr_requires_observation",
        "merge_request_requires_merge_outcome",
        "published_pr_requires_review_current_head",
        "head_change_requires_ci_status_current_head",
        "merge_request_requires_approved_current_head",
        "merge_request_requires_passing_ci_current_head",
        "guidance_enqueue_requires_acceptance_or_abandonment",
    }
    if rule_ids != expected_rules:
        raise ContractError(f"expected-event contract: rules must be {sorted(expected_rules)}")
    return rule_ids


def validate_fixtures(fixtures: dict[str, Any], event_types: set[str], rule_ids: set[str]) -> None:
    require_keys(fixtures, {"fixture_id", "version", "scenarios", "invariant_coverage", "required_event_contracts"}, "fixture bundle")
    if fixtures["version"] != 1:
        raise ContractError("fixture bundle: version must be 1")
    scenarios = fixtures["scenarios"]
    scenario_ids = {scenario["scenario_id"] for scenario in scenarios}
    if scenario_ids != REQUIRED_SCENARIOS:
        raise ContractError(f"fixture bundle: scenarios must be {sorted(REQUIRED_SCENARIOS)}")
    for scenario in scenarios:
        context = f"fixture scenario {scenario['scenario_id']}"
        if not set(scenario["events"]).issubset(event_types):
            unknown = sorted(set(scenario["events"]) - event_types)
            raise ContractError(f"{context}: undeclared events: {', '.join(unknown)}")
        if not scenario["harnesses"]:
            raise ContractError(f"{context}: at least one harness is required")
    gap_fixture = next(scenario for scenario in scenarios if scenario["scenario_id"] == "partial_sink_and_gap")
    if gap_fixture.get("expected_status") != "partial" or not isinstance(gap_fixture.get("missing_sequence"), int):
        raise ContractError("fixture bundle: partial sink fixture must include a sequence gap")
    privacy_fixture = next(scenario for scenario in scenarios if scenario["scenario_id"] == "privacy_boundary_inputs")
    required_sensitive_inputs = {"conversation", "reasoning", "payload", "absolute_path", "url", "branch", "fake_token", "secret_like_custom_payload"}
    if set(privacy_fixture.get("sensitive_input_classes", [])) != required_sensitive_inputs:
        raise ContractError("fixture bundle: privacy fixture is missing a sensitive input class")

    coverage = fixtures["invariant_coverage"]
    if set(coverage) != REQUIRED_INVARIANTS or any(not coverage[key] for key in coverage):
        raise ContractError("fixture bundle: every I1-I8 invariant needs a non-empty scenario mapping")
    if set(fixtures["required_event_contracts"]) != rule_ids:
        raise ContractError("fixture bundle: required event contracts do not match the registry")


def validate(root: Path) -> None:
    registry = load_json(root / "docs/observability/event-registry.json")
    contract = load_json(root / "docs/observability/expected-events.v1.json")
    measurement = load_json(root / "docs/observability/measurement-contract.v1.json")
    fixtures = load_json(root / "docs/observability/fixtures/phase0-contract-fixtures.json")
    event_types = validate_registry(registry)
    rule_ids = validate_expected_events(contract, event_types)
    required_measurement_keys = {
        "contract_id",
        "version",
        "primary_unit",
        "required_confound_controls",
        "required_provenance",
        "causal_comparison_rule",
    }
    missing_measurement_keys = required_measurement_keys - measurement.keys()
    if missing_measurement_keys:
        raise ContractError(
            "measurement contract missing keys: " + ", ".join(sorted(missing_measurement_keys))
        )
    if measurement["primary_unit"] != "session":
        raise ContractError("measurement primary unit must be session")
    if len(measurement["required_confound_controls"]) < 6:
        raise ContractError("measurement contract must declare all confound controls")
    validate_fixtures(fixtures, event_types, rule_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        validate(args.root.resolve())
    except (ContractError, OSError) as error:
        print(f"observability contract validation failed: {error}")
        return 1
    print("observability contracts and Phase 0 fixtures are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
