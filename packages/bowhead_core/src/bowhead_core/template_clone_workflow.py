"""Shared section representation for template export."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ComposedSection:
    """Section data consumed by the current template renderer."""

    id: str
    title: str
    chapter_tag: str
    kind: str  # Matches schema kind (transcribe)
    data: Any  # Original data (table rows / metrics)
    narrative: str  # narrative bullets
    error: str = ""  # Failure reason
    needs_user_input: bool = False  # deferred state: UI asks user to supply data manually

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "chapter_tag": self.chapter_tag,
            "kind": self.kind,
            "data": self.data,
            "narrative": self.narrative,
            "error": self.error,
            "needs_user_input": self.needs_user_input,
        }
