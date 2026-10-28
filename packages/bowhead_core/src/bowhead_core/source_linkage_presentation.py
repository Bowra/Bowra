"""User-facing source-linkage projection for the B1a trust-copy correction."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_JSON_SAFE_INTEGER = (1 << 53) - 1


def normalize_nonnegative_count(value: object) -> int | None:
    """Normalize a JSON number without accepting bools, strings, or unsafe integers."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_JSON_SAFE_INTEGER else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0 or value > MAX_JSON_SAFE_INTEGER:
            return None
        return int(value)
    return None


@dataclass(frozen=True, slots=True)
class SourceLinkagePresentation:
    citation_count: int | None
    figure_review_count: int | None
    source_count: int | None

    @property
    def visible(self) -> bool:
        return any(value is not None and value > 0 for value in (self.citation_count, self.figure_review_count, self.source_count))


def project_source_linkage(value: object) -> SourceLinkagePresentation:
    """Project legacy trust counts into three independent user-facing facts.

    ``verified_pct`` is deliberately not read. A malformed field does not erase the
    other two fields and never becomes an implicit zero.
    """

    raw: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    return SourceLinkagePresentation(
        citation_count=normalize_nonnegative_count(raw.get("verified")),
        figure_review_count=normalize_nonnegative_count(raw.get("unverified")),
        source_count=normalize_nonnegative_count(raw.get("sources")),
    )


__all__ = [
    "MAX_JSON_SAFE_INTEGER",
    "SourceLinkagePresentation",
    "normalize_nonnegative_count",
    "project_source_linkage",
]
