"""Auditable model resolution over a Rust-provided catalog snapshot."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tl_loop.client.effects import ToolResult

THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
_DATE_SUFFIX = re.compile(r"-\d{8}$")


class ModelResolutionError(ValueError):
    """Raised when a requested model cannot be resolved safely."""


@dataclass(frozen=True)
class CatalogModel:
    """One normalized model record emitted by the Rust catalog boundary."""

    harness: str
    model_id: str
    provider: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ModelCatalog:
    """Normalized catalog snapshot; no provider-specific parsing occurs here."""

    models: tuple[CatalogModel, ...]

    @classmethod
    def from_payload(cls, payload: object) -> ModelCatalog:
        """Decode the normalized effect payload or fixture shape."""
        root = _mapping(payload, "catalog")
        raw_models = root.get("models")
        if not isinstance(raw_models, list):
            raise ModelResolutionError("catalog.models: must be an array")
        return cls(tuple(_catalog_model(item, index) for index, item in enumerate(raw_models)))

    @classmethod
    def from_effect(cls, result: ToolResult) -> ModelCatalog:
        """Consume a successful effect-client result without inspecting raw CLI output."""
        if result.success is not True or result.result is None:
            raise ModelResolutionError(result.error or "model catalog effect failed")
        return cls.from_payload(result.result)

    @classmethod
    def from_fixture(cls, path: str | Path) -> ModelCatalog:
        """Load a committed normalized catalog snapshot for replayable tests."""
        target = Path(path)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelResolutionError(f"{target}: invalid catalog fixture: {error}") from error
        return cls.from_payload(payload)

    def for_harness(self, harness: str) -> tuple[CatalogModel, ...]:
        """Return records belonging to the selected harness, preserving catalog order."""
        harness_name = harness.split("/", 1)[0].lower()
        return tuple(model for model in self.models if model.harness.lower() == harness_name)


@dataclass(frozen=True)
class ModelChoice:
    """Resolved model and the ladder rung that selected it."""

    model_id: str
    thinking_level: str | None
    ladder_rung_used: str
    harness: str


def select_model(
    harness: str,
    catalog_or_requested: ModelCatalog | Mapping[str, object] | str | None,
    requested_or_catalog: str | ModelCatalog | Mapping[str, object] | None = None,
    *,
    provider_default: str | None = None,
    exact_reference: bool = False,
) -> ModelChoice:
    """Resolve a model using exact, alias, dated, default, then fallback rungs.

    Both ``select_model(harness, catalog, requested)`` and
    ``select_model(harness, requested, catalog)`` are accepted at this boundary.
    """
    catalog, requested = _normalize_arguments(catalog_or_requested, requested_or_catalog)
    records = catalog.for_harness(harness)
    if not records:
        raise ModelResolutionError(f"no models available for harness {harness!r}")
    base_reference, thinking_level, exact_full = _parse_reference(requested, records, harness)
    if base_reference is not None:
        exact = _find_exact(records, base_reference, harness)
        if exact is not None:
            return _choice(exact, thinking_level, "exact_reference", harness)
        if requested and (exact_reference or _looks_canonical(base_reference, harness)):
            raise ModelResolutionError(f"exact model reference {requested!r} is absent from {harness!r}")
        if requested and exact_full:
            raise ModelResolutionError(f"exact model reference {requested!r} is absent from {harness!r}")
        if requested:
            match = _find_pattern(records, base_reference)
            if match is not None:
                return _choice(match, thinking_level, _pattern_rung(records, base_reference), harness)
    if provider_default:
        match = _find_exact(records, provider_default, harness)
        if match is not None:
            return _choice(match, None, "provider_default", harness)
    return _choice(records[0], None, "fallback", harness)


def resolve_model(
    harness: str,
    catalog_or_requested: ModelCatalog | Mapping[str, object] | str | None,
    requested_or_catalog: str | ModelCatalog | Mapping[str, object] | None = None,
    *,
    provider_default: str | None = None,
    exact_reference: bool = False,
) -> ModelChoice:
    """Named alias for callers that use the resolver terminology."""
    return select_model(
        harness,
        catalog_or_requested,
        requested_or_catalog,
        provider_default=provider_default,
        exact_reference=exact_reference,
    )


def parse_thinking_suffix(reference: str) -> tuple[str, str | None]:
    """Separate a recognized ``:thinking_level`` suffix from a model reference."""
    separator = reference.rfind(":")
    if separator == -1:
        return reference, None
    suffix = reference[separator + 1:]
    if suffix not in THINKING_LEVELS:
        return reference, None
    return reference[:separator], suffix


def _normalize_arguments(
    first: ModelCatalog | Mapping[str, object] | str | None,
    second: str | ModelCatalog | Mapping[str, object] | None,
) -> tuple[ModelCatalog, str | None]:
    if isinstance(first, ModelCatalog):
        return first, cast(str | None, second)
    if isinstance(first, Mapping):
        return ModelCatalog.from_payload(first), cast(str | None, second)
    if isinstance(second, ModelCatalog):
        return second, first
    if isinstance(second, Mapping):
        return ModelCatalog.from_payload(second), first
    raise TypeError("select_model requires a ModelCatalog and optional model reference")


def _parse_reference(
    requested: str | None,
    records: Sequence[CatalogModel],
    harness: str,
) -> tuple[str | None, str | None, bool]:
    if requested is None:
        return None, None, False
    trimmed = requested.strip()
    full = _find_exact(records, trimmed, harness) is not None
    base, thinking = parse_thinking_suffix(trimmed)
    return base, thinking, full and thinking is None


def _find_exact(
    records: Sequence[CatalogModel], reference: str, harness: str
) -> CatalogModel | None:
    normalized = reference.lower()
    harness_name = harness.split("/", 1)[0].lower()
    for record in records:
        identifiers = {
            record.model_id,
            record.name,
            f"{record.harness}/{record.model_id}",
            f"{harness_name}/{record.model_id}",
        }
        if record.provider:
            identifiers.add(f"{record.provider}/{record.model_id}")
        if any(identifier and identifier.lower() == normalized for identifier in identifiers):
            return record
    return None


def _find_pattern(records: Sequence[CatalogModel], pattern: str) -> CatalogModel | None:
    needle = pattern.lower()
    matches = tuple(
        record
        for record in records
        if needle in record.model_id.lower() or (record.name and needle in record.name.lower())
    )
    if not matches:
        return None
    aliases = tuple(record for record in matches if _is_alias(record.model_id))
    if aliases:
        return max(aliases, key=lambda record: record.model_id.lower())
    dated = tuple(record for record in matches if _DATE_SUFFIX.search(record.model_id))
    return max(dated, key=lambda record: record.model_id.lower()) if dated else matches[0]


def _pattern_rung(records: Sequence[CatalogModel], pattern: str) -> str:
    matches = tuple(record for record in records if pattern.lower() in record.model_id.lower())
    return "alias_preferred" if any(_is_alias(record.model_id) for record in matches) else "latest_dated"


def _is_alias(model_id: str) -> bool:
    return model_id.endswith("-latest") or _DATE_SUFFIX.search(model_id) is None


def _looks_canonical(reference: str, harness: str) -> bool:
    return "/" in reference and reference.split("/", 1)[0].lower() == harness.split("/", 1)[0].lower()


def _choice(
    record: CatalogModel,
    thinking_level: str | None,
    rung: str,
    harness: str,
) -> ModelChoice:
    return ModelChoice(record.model_id, thinking_level, rung, harness)


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ModelResolutionError(f"{path}: must be an object")
    return cast(Mapping[str, object], value)


def _catalog_model(value: object, index: int) -> CatalogModel:
    path = f"catalog.models[{index}]"
    record = _mapping(value, path)
    harness = _string(record, "harness", path)
    model_id = _string(record, "model_id", path)
    provider = _optional_string(record, "provider", path)
    name = _optional_string(record, "name", path)
    return CatalogModel(harness, model_id, provider, name)


def _string(record: Mapping[str, object], key: str, path: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ModelResolutionError(f"{path}.{key}: must be a non-empty string")
    return value


def _optional_string(record: Mapping[str, object], key: str, path: str) -> str | None:
    value = record.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ModelResolutionError(f"{path}.{key}: must be null or a non-empty string")
    return value


__all__ = [
    "THINKING_LEVELS",
    "CatalogModel",
    "ModelCatalog",
    "ModelChoice",
    "ModelResolutionError",
    "parse_thinking_suffix",
    "resolve_model",
    "select_model",
]
