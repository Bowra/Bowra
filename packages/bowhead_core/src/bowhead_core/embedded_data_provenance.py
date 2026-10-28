"""Fail-closed presentation contract for embedded HTML data provenance."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from bowhead_core.source_linkage_presentation import MAX_JSON_SAFE_INTEGER

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
_MISSING = object()

EmbeddedDataStatus = Literal["injected", "computed_unchecked"]


@dataclass(frozen=True, slots=True)
class EmbeddedDataProvenance:
    status: EmbeddedDataStatus
    source_indexes: tuple[int, ...]

    @property
    def has_sources(self) -> bool:
        return bool(self.source_indexes)


def _is_finite_json_number(value: int | float) -> bool:
    if isinstance(value, int):
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    return math.isfinite(value)


def _is_json_value(value: object, *, depth: int, ancestors: set[int], budget: list[int]) -> bool:
    if depth > MAX_JSON_DEPTH:
        return False
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES:
        return False

    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _is_finite_json_number(value)

    if type(value) is list:
        identity = id(value)
        if identity in ancestors:
            return False
        ancestors.add(identity)
        try:
            return all(_is_json_value(item, depth=depth + 1, ancestors=ancestors, budget=budget) for item in value)
        finally:
            ancestors.remove(identity)

    if type(value) is dict:
        identity = id(value)
        if identity in ancestors:
            return False
        if any(not isinstance(key, str) for key in value):
            return False
        ancestors.add(identity)
        try:
            return all(_is_json_value(item, depth=depth + 1, ancestors=ancestors, budget=budget) for item in value.values())
        finally:
            ancestors.remove(identity)

    return False


def _is_nonempty_structured(value: object) -> bool:
    if type(value) not in (dict, list) or not value:
        return False
    return _is_json_value(value, depth=0, ancestors=set(), budget=[0])


def _normalize_source_indexes(value: object) -> tuple[int, ...]:
    if type(value) is not list:
        return ()
    out: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            normalized = item
        elif isinstance(item, float) and math.isfinite(item) and item.is_integer():
            normalized = int(item)
        else:
            continue
        if normalized <= 0 or normalized > MAX_JSON_SAFE_INTEGER or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def project_embedded_data_provenance(value: object) -> EmbeddedDataProvenance:
    raw: Mapping[str, object] = value if isinstance(value, Mapping) else {}
    unverified = raw.get("unverified", _MISSING)
    allows_injection = unverified is _MISSING or unverified is None or unverified is False
    injected = allows_injection and _is_nonempty_structured(raw.get("data", _MISSING))
    return EmbeddedDataProvenance(
        status="injected" if injected else "computed_unchecked",
        source_indexes=_normalize_source_indexes(raw.get("sources")),
    )


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "EmbeddedDataProvenance",
    "EmbeddedDataStatus",
    "project_embedded_data_provenance",
]
